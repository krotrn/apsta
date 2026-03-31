import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import apsta_cli.common as common


class CommonRuntimeTests(unittest.TestCase):
    def test_log_event_writes_json_record(self):
        with tempfile.TemporaryDirectory() as td:
            original_log_path = common.LOG_PATH
            common.LOG_PATH = Path(td) / "apsta.log"
            try:
                common.log_event("INFO", "unit-test", answer=42, nested={"ok": True})
            finally:
                log_path = common.LOG_PATH
                common.LOG_PATH = original_log_path

            lines = log_path.read_text(encoding="utf-8").strip().splitlines()
            self.assertTrue(lines)
            record = json.loads(lines[-1])
            self.assertEqual(record["level"], "INFO")
            self.assertEqual(record["event"], "unit-test")
            self.assertEqual(record["fields"]["answer"], 42)
            self.assertEqual(record["fields"]["nested"]["ok"], True)

    def test_command_lock_blocks_other_process(self):
        with tempfile.TemporaryDirectory() as td:
            original_lock_path = common.LOCK_PATH
            lock_path = Path(td) / "apsta.lock"
            common.LOCK_PATH = lock_path
            try:
                with common.command_lock("primary"):
                    probe = subprocess.run(
                        [
                            "python3",
                            "-c",
                            (
                                "import sys; from fcntl import flock, LOCK_EX, LOCK_NB; "
                                "f=open(sys.argv[1],'a+'); "
                                "\ntry:\n flock(f.fileno(), LOCK_EX|LOCK_NB); sys.exit(1)"
                                "\nexcept BlockingIOError:\n sys.exit(0)"
                            ),
                            str(lock_path),
                        ],
                        capture_output=True,
                        text=True,
                    )
                self.assertEqual(probe.returncode, 0)
            finally:
                common.LOCK_PATH = original_lock_path


if __name__ == "__main__":
    unittest.main()
