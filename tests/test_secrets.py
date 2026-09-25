import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from apsta_cli import common


class SecretsStorageTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config_path = Path(tmp.name) / "config.json"
        self.secrets_path = Path(tmp.name) / "secrets.json"
        for name, value in (("CONFIG_PATH", self.config_path), ("SECRETS_PATH", self.secrets_path)):
            patcher = patch.object(common, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_save_splits_password_into_root_only_file(self):
        cfg = common.normalize_config({})
        common.set_profile_field(cfg, "password", "hunter2hunter2")
        common.save_config(cfg)

        self.assertNotIn("hunter2hunter2", self.config_path.read_text())
        self.assertEqual(stat.S_IMODE(self.secrets_path.stat().st_mode), 0o600)
        self.assertEqual(common.load_config()["password"], "hunter2hunter2")

    @patch("apsta_cli.common.os.geteuid", return_value=0)
    def test_legacy_plaintext_config_is_migrated_as_root(self, _euid):
        self.config_path.write_text(json.dumps({"ssid": "Old", "password": "legacy-pass"}))

        cfg = common.load_config()

        self.assertEqual(cfg["password"], "legacy-pass")
        self.assertNotIn("legacy-pass", self.config_path.read_text())
        self.assertEqual(json.loads(self.secrets_path.read_text())["default"], "legacy-pass")

    @unittest.skipIf(os.geteuid() == 0, "root can read any file")
    def test_unreadable_secrets_hide_password_instead_of_defaulting(self):
        common.save_config(common.normalize_config({}))
        self.secrets_path.chmod(0)

        cfg = common.load_config()

        self.assertIsNone(cfg["password"])
        self.assertEqual(cfg["ssid"], "apsta-hotspot")


if __name__ == "__main__":
    unittest.main()
