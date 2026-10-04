import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from apsta_cli.core import fsutil, lock, log, output, paths, shell
from apsta_cli.core.errors import ApstaError, SetupError
from tests.support import as_root, isolate_paths


class ShellTests(unittest.TestCase):
    def test_runs_argv_without_shell(self):
        result = shell.run([sys.executable, "-c", "import sys; print(sys.argv[1])", "$(echo hi); rm -rf /"])
        self.assertTrue(result.ok)
        self.assertEqual(result.stdout.strip(), "$(echo hi); rm -rf /")

    def test_missing_command(self):
        result = shell.run(["apsta-definitely-missing-tool"])
        self.assertEqual(result.returncode, 127)
        self.assertIn("not found", result.message())

    def test_timeout(self):
        result = shell.run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.2)
        self.assertEqual(result.returncode, 124)

    def test_input_and_out(self):
        self.assertEqual(shell.out([sys.executable, "-c", "print(input())"], input="hello\n"), "hello")
        self.assertEqual(shell.out([sys.executable, "-c", "raise SystemExit(3)"]), "")

    def test_daemonizing_command_does_not_block_on_inherited_pipes(self):
        import time

        script = "import os, sys, time\nif os.fork():\n    print('parent'); sys.exit(0)\ntime.sleep(5)\n"
        started = time.monotonic()
        result = shell.run([sys.executable, "-c", script], daemonizes=True, timeout=4)
        self.assertTrue(result.ok)
        self.assertEqual(result.stdout.strip(), "parent")
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(shell.run(["apsta-missing-tool"], daemonizes=True).returncode, 127)
        self.assertEqual(
            shell.run([sys.executable, "-c", "import time; time.sleep(3)"], daemonizes=True, timeout=0.2).returncode,
            124,
        )

    def test_check(self):
        with self.assertRaises(SetupError) as ctx:
            shell.Result(["x"], 1, "", "boom").check("Doing x")
        self.assertIn("Doing x failed: boom", ctx.exception.message)
        self.assertEqual(shell.Result(["x"], 2, "", "").message(), "exit code 2")

    def test_pinned_search_path(self):
        with tempfile.TemporaryDirectory() as td:
            tool = Path(td) / "fancytool"
            tool.write_text("#!/bin/sh\necho fake\n")
            tool.chmod(0o755)
            with mock.patch.dict(os.environ, {"APSTA_PATH": td}):
                self.assertEqual(shell.which("fancytool"), str(tool))
                self.assertIsNone(shell.which("ls"))
                self.assertEqual(shell.out(["fancytool"]), "fake")
                self.assertTrue(shell.have("fancytool"))


class FsUtilTests(unittest.TestCase):
    def test_atomic_write_sets_mode_and_replaces(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "sub" / "file"
            fsutil.atomic_write(target, "one", mode=0o600)
            fsutil.atomic_write(target, "two", mode=0o600)
            self.assertEqual(target.read_text(), "two")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(os.listdir(target.parent), ["file"])  # no temp files left

    def test_atomic_write_cleans_up_on_error(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch("os.replace", side_effect=OSError("disk")):
                with self.assertRaises(OSError):
                    fsutil.atomic_write(Path(td) / "f", "x")
            self.assertEqual(os.listdir(td), [])

    def test_remove_missing_is_fine(self):
        fsutil.remove(Path("/nonexistent/apsta/file"))


class LogTests(unittest.TestCase):
    def test_root_writes_json_record(self):
        isolate_paths(self)
        as_root(self)
        log.event("INFO", "unit", nested={"a": (1, 2)}, obj=object)
        record = json.loads(paths.LOG_PATH.read_text().splitlines()[-1])
        self.assertEqual(record["event"], "unit")
        self.assertEqual(record["fields"]["nested"]["a"], [1, 2])
        self.assertIsInstance(record["fields"]["obj"], str)

    def test_unprivileged_does_not_write(self):
        isolate_paths(self)
        with mock.patch("os.geteuid", return_value=1000), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("APSTA_LOG_PATH", None)
            log.event("INFO", "unit")
        self.assertFalse(paths.LOG_PATH.exists())

    def test_rotation(self):
        isolate_paths(self)
        as_root(self)
        paths.LOG_PATH.write_text("x" * (log.MAX_BYTES + 1))
        log.event("INFO", "after-rotate")
        self.assertTrue(paths.LOG_PATH.with_name(paths.LOG_PATH.name + ".1").exists())
        self.assertIn("after-rotate", paths.LOG_PATH.read_text())

    def test_debug_flag(self):
        with mock.patch.dict(os.environ, {"APSTA_DEBUG": "yes"}):
            self.assertTrue(log.debug_enabled())
        with mock.patch.dict(os.environ, {"APSTA_DEBUG": "0"}):
            self.assertFalse(log.debug_enabled())


class OutputTests(unittest.TestCase):
    def test_no_color_when_not_tty(self):
        self.assertEqual(output.C.RED, "")  # unittest stdout is not a TTY

    def test_color_on_tty(self):
        with mock.patch.object(sys.stdout, "isatty", return_value=True), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NO_COLOR", None)
            self.assertEqual(output.C.RED, "\033[91m")
        with self.assertRaises(AttributeError):
            output.C.PURPLE  # noqa: B018

    def test_messages_go_to_right_stream(self):
        isolate_paths(self)
        with mock.patch("sys.stdout") as out, mock.patch("sys.stderr") as errs:
            out.isatty.return_value = False
            output.ok("fine")
            output.err("bad")
            output.warn("hmm")
            output.info("fyi")
            output.head("H")
            output.detail("d")
            output.blank()
            with mock.patch.dict(os.environ, {"APSTA_DEBUG": "1"}):
                output.dbg("debugging", k=1)
        self.assertTrue(out.write.called)
        self.assertTrue(errs.write.called)


class LockTests(unittest.TestCase):
    def test_lock_excludes_other_process(self):
        isolate_paths(self)
        probe = (
            "import sys; from fcntl import flock, LOCK_EX, LOCK_NB\n"
            "f = open(sys.argv[1], 'a+')\n"
            "try:\n flock(f.fileno(), LOCK_EX | LOCK_NB); sys.exit(1)\n"
            "except BlockingIOError:\n sys.exit(0)\n"
        )
        with lock.command_lock("test"):
            result = subprocess.run([sys.executable, "-c", probe, str(paths.LOCK_PATH)])
        self.assertEqual(result.returncode, 0)
        self.assertIn("action=test", paths.LOCK_PATH.read_text())

    def test_lock_times_out(self):
        isolate_paths(self)
        paths.RUN_DIR.mkdir(parents=True)
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys,time; from fcntl import flock, LOCK_EX; f=open(sys.argv[1],'a+'); flock(f.fileno(), LOCK_EX);"
                " print('locked', flush=True); time.sleep(5)",
                str(paths.LOCK_PATH),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(lambda: (holder.kill(), holder.wait(), holder.stdout.close()))
        holder.stdout.readline()
        with self.assertRaises(ApstaError):
            with lock.command_lock("test", wait_seconds=0.2):
                pass


if __name__ == "__main__":
    unittest.main()
