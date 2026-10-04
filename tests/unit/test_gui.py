"""GUI logic that doesn't need GTK: share strings, formatting, and the CLI backend."""

import json
import subprocess
import unittest
from unittest import mock

from apsta_gui import backend, helpers


def completed(rc=0, out="", err=""):
    return subprocess.CompletedProcess([], rc, out, err)


class HelperTests(unittest.TestCase):
    def test_wifi_share_string_escapes(self):
        self.assertEqual(helpers.wifi_share_string("My;Net", 'pa:ss,"w'), 'WIFI:T:WPA;S:My\\;Net;P:pa\\:ss\\,\\"w;;')
        self.assertEqual(helpers.wifi_share_string("", "x"), "")

    def test_format_clients(self):
        self.assertEqual(helpers.format_clients([]), "No clients connected.")
        text = helpers.format_clients([{"hostname": "", "mac": "aa", "ip": "", "limit_kbps": 50}])
        self.assertIn("50 Kbps", text)
        self.assertIn(" - ", text)

    def test_band_label(self):
        self.assertEqual(helpers.band_label("a"), "5 GHz")
        self.assertEqual(helpers.band_label("bg"), "2.4 GHz")


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.b = backend.ApstaBackend("/usr/bin/apsta")

    def test_privileged_calls_apsta_directly_with_secret_on_stdin(self):
        with mock.patch.object(backend.subprocess, "run", return_value=completed()) as run:
            result = self.b.start("Cafe", "secret123", allow_disconnect=True)
        argv = run.call_args[0][0]
        self.assertEqual(argv[:2], ["pkexec", "/usr/bin/apsta"])
        self.assertNotIn("sh", argv)
        self.assertNotIn("secret123", " ".join(argv))
        self.assertEqual(run.call_args.kwargs["input"], "secret123\n")
        self.assertIn("--allow-disconnect", argv)
        self.assertTrue(result.ok)

    def test_errors(self):
        cases = [
            (completed(126), "Authentication cancelled."),
            (completed(127), "apsta not found."),
            (completed(1, err="  ✘  Could not start\n  hint"), "Could not start"),
        ]
        for proc, message in cases:
            with mock.patch.object(backend.subprocess, "run", return_value=proc):
                self.assertEqual(self.b.stop().message, message)
        with mock.patch.object(backend.subprocess, "run", side_effect=FileNotFoundError):
            self.assertIn("pkexec", self.b.stop().message)
        with mock.patch.object(backend.subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 1)):
            self.assertIn("timed out", self.b.stop().message)

    def test_status_and_text(self):
        with mock.patch.object(backend.subprocess, "run", return_value=completed(out=json.dumps({"active": True}))):
            self.assertEqual(self.b.status(), {"active": True})
        with mock.patch.object(backend.subprocess, "run", return_value=completed(1)):
            self.assertEqual(self.b.status(), {})
        with mock.patch.object(backend.subprocess, "run", return_value=completed(out="\x1b[1mhi\x1b[0m")):
            self.assertEqual(self.b.text("detect"), "hi")
        with mock.patch.object(backend.subprocess, "run", side_effect=OSError("nope")):
            self.assertEqual(self.b.text("detect"), "nope")

    def test_argument_shapes(self):
        with mock.patch.object(backend.subprocess, "run", return_value=completed()) as run:
            self.b.save_config("S", "", "")
            self.assertIn("interface=auto", run.call_args[0][0])
            self.assertIsNone(run.call_args.kwargs["input"])
            self.b.use_profile("travel")
            self.b.disconnect("phone", block=True)
            self.assertEqual(run.call_args[0][0][-3:], ["disconnect", "phone", "--block"])
            self.b.limit("phone", 100)
            self.b.enable_service()
            self.b.disable_service()
        self.assertFalse(backend.ApstaBackend("/nonexistent/apsta").available())


if __name__ == "__main__":
    unittest.main()
