import sys
import types
import unittest
import unittest.mock as mock
from types import SimpleNamespace

from apsta_cli.cmd import qr
from apsta_cli.config import model, store
from apsta_cli.core.errors import ApstaError
from tests.support import as_root, isolate_paths


class ShareStringTests(unittest.TestCase):
    def test_escapes_and_hidden(self):
        self.assertEqual(qr.wifi_share_string("My;Net", 'pa:ss,"w'), r"WIFI:T:WPA;S:My\;Net;P:pa\:ss\,\"w;;")
        self.assertEqual(qr.wifi_share_string("N", "p", hidden=True), "WIFI:T:WPA;S:N;P:p;H:true;;")


def fake_qrcode():
    module = types.ModuleType("qrcode")
    module.constants = SimpleNamespace(ERROR_CORRECT_M=0)

    class QRCode:
        def __init__(self, **kw):
            self.data = None

        def add_data(self, data):
            self.data = data

        def make(self, fit):
            pass

        def print_ascii(self, out, invert):
            out.write(f"<QR {self.data}>\n")

    module.QRCode = QRCode
    return module


class QrCommandTests(unittest.TestCase):
    def setUp(self):
        isolate_paths(self)
        as_root(self)
        self.out = mock.patch("sys.stdout").start()
        mock.patch("sys.stderr").start()
        self.addCleanup(mock.patch.stopall)

    def written(self):
        return "".join(c.args[0] for c in self.out.write.call_args_list)

    def save(self, **settings):
        config = store.load()
        for key, value in settings.items():
            model.set_field(config, key, value)
        store.save(config)

    def test_prints_qr_with_hidden_flag(self):
        self.save(ssid="Cafe", password="secret123", hidden="yes")
        with mock.patch.dict(sys.modules, {"qrcode": fake_qrcode()}):
            self.assertEqual(qr.cmd_qr(SimpleNamespace()), 0)
        self.assertIn("<QR WIFI:T:WPA;S:Cafe;P:secret123;H:true;;>", self.written())
        self.assertIn("(hidden)", self.written())

    def test_without_qrcode_module_prints_the_payload(self):
        self.save(password="secret123")
        with mock.patch.dict(sys.modules, {"qrcode": None}):
            self.assertIsNone(qr.render_qr("x"))
            self.assertEqual(qr.cmd_qr(SimpleNamespace()), 0)
        self.assertIn("QR payload: WIFI:T:WPA;", self.written())

    def test_no_password_yet(self):
        with self.assertRaises(ApstaError):
            qr.cmd_qr(SimpleNamespace())


if __name__ == "__main__":
    unittest.main()
