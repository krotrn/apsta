import json
import os
import stat
import unittest
import unittest.mock as mock

from apsta_cli.config import model, store, validate
from apsta_cli.core import paths
from apsta_cli.core.errors import UsageError
from tests.support import as_root, isolate_paths


class ValidateTests(unittest.TestCase):
    def test_ssid(self):
        self.assertEqual(validate.ssid("Home"), "Home")
        for bad in ("", "x" * 33, "a\nb", "é" * 17):
            with self.assertRaises(UsageError):
                validate.ssid(bad)

    def test_password(self):
        self.assertEqual(validate.password("12345678"), "12345678")
        self.assertEqual(validate.password("ab" * 32), "ab" * 32)  # 64 hex digits = raw PSK
        for bad in ("short", "x" * 64, "pässwörd1", "line\nbreak"):
            with self.assertRaises(UsageError):
                validate.password(bad)

    def test_band_channel_interface_profile(self):
        self.assertEqual(validate.band("a"), "a")
        self.assertEqual(validate.channel("06"), "6")
        self.assertIsNone(validate.interface("auto"))
        self.assertEqual(validate.interface("wlx00c0ca123456"), "wlx00c0ca123456")
        self.assertEqual(validate.profile_name(" travel "), "travel")
        for fn, bad in (
            (validate.band, "6g"),
            (validate.channel, "0"),
            (validate.channel, "x"),
            (validate.interface, "a;b"),
            (validate.profile_name, "has space"),
        ):
            with self.assertRaises(UsageError):
                fn(bad)

    def test_flag_and_macs(self):
        self.assertTrue(validate.flag("Yes"))
        self.assertFalse(validate.flag("off"))
        with self.assertRaises(UsageError):
            validate.flag("maybe")
        self.assertEqual(validate.mac("AA-BB-CC-DD-EE-FF"), "aa:bb:cc:dd:ee:ff")
        with self.assertRaises(UsageError):
            validate.mac("aa:bb:cc")
        self.assertEqual(
            validate.mac_list("AA:BB:CC:DD:EE:FF, 11-22-33-44-55-66 aa:bb:cc:dd:ee:ff"),
            ["aa:bb:cc:dd:ee:ff", "11:22:33:44:55:66"],
        )
        self.assertIsNone(validate.mac_list(" , "))
        self.assertFalse(validate.profile_value("hidden", "none"))
        self.assertIsNone(validate.profile_value("allowed_macs", ""))

    def test_profile_value(self):
        self.assertIsNone(validate.profile_value("password", ""))
        self.assertIsNone(validate.profile_value("interface", "none"))
        with self.assertRaises(UsageError):
            validate.profile_value("ssid", "")
        with self.assertRaises(UsageError):
            validate.profile_value("bogus", "x")


class ModelTests(unittest.TestCase):
    def test_defaults_have_no_password(self):
        cfg = model.normalize({})
        self.assertIsNone(cfg["password"])
        self.assertEqual(cfg["active_profile"], "default")

    def test_hidden_and_allowlist_defaults_and_coercion(self):
        config = model.normalize({})
        self.assertEqual((config["hidden"], config["allowed_macs"]), (False, None))
        config = model.normalize({"hidden": "yes", "allowed_macs": ["AA:BB:CC:DD:EE:FF"]})
        self.assertEqual((config["hidden"], config["allowed_macs"]), (True, ["aa:bb:cc:dd:ee:ff"]))
        config = model.normalize({"hidden": "bogus", "allowed_macs": "not-a-mac"})  # hand-edited junk
        self.assertEqual((config["hidden"], config["allowed_macs"]), (False, None))
        model.set_field(config, "allowed_macs", "aa:bb:cc:dd:ee:ff")
        model.set_field(config, "hidden", "yes")
        self.assertEqual((config["hidden"], config["allowed_macs"]), (True, ["aa:bb:cc:dd:ee:ff"]))

    def test_method_and_channel_settings(self):
        config = model.normalize({})
        self.assertEqual((config["method"], config["channel"]), ("auto", "auto"))
        model.set_field(config, "method", "P2P")
        model.set_field(config, "channel", "149")
        self.assertEqual((config["method"], config["channel"]), ("p2p", "149"))
        model.set_field(config, "channel", "auto")
        model.set_field(config, "method", "")  # empty: back to the default
        self.assertEqual((config["method"], config["channel"]), ("auto", "auto"))
        self.assertEqual(validate.method("nmcli-force"), "nmcli-single")
        for bad in (("method", "magic"), ("channel", "0"), ("channel", "x")):
            with self.assertRaises(UsageError):
                model.set_field(config, *bad)
        junk = model.normalize({"method": "magic", "channel": "nope"})  # hand-edited
        self.assertEqual((junk["method"], junk["channel"]), ("auto", "auto"))

    def test_old_default_channel_means_auto(self):
        # apsta <= 0.8 wrote channel "6" without anyone choosing it, and no "method".
        self.assertEqual(model.normalize({"channel": "6"})["channel"], "auto")
        self.assertEqual(model.normalize({"channel": "11"})["channel"], "11")  # a real choice is kept
        self.assertEqual(model.normalize({"channel": "6", "method": "auto"})["channel"], "6")

    def test_legacy_top_level_values_become_default_profile(self):
        cfg = model.normalize({"ssid": "Old", "password": "changeme123", "ap_interface": "x"})
        self.assertEqual(cfg["profiles"]["default"]["ssid"], "Old")
        self.assertIsNone(cfg["password"])  # the old shared default is not a password
        self.assertNotIn("ap_interface", cfg)

    def test_invalid_active_and_names(self):
        cfg = model.normalize({"profiles": {"b": {}, " ": {}, "a": "junk"}, "active_profile": "zzz"})
        self.assertEqual(cfg["active_profile"], "a")
        self.assertEqual(model.profile_names(cfg), ["a", "b"])

    def test_profile_lifecycle(self):
        cfg = model.normalize({})
        model.set_field(cfg, "ssid", "Base")
        model.create_profile(cfg, "travel")
        self.assertEqual(cfg["profiles"]["travel"]["ssid"], "Base")
        model.use_profile(cfg, "travel")
        model.set_field(cfg, "ssid", "Road")
        self.assertEqual(cfg["ssid"], "Road")
        self.assertEqual(model.active_profile(cfg)["ssid"], "Road")
        with self.assertRaises(UsageError):
            model.delete_profile(cfg, "travel")
        model.use_profile(cfg, "default")
        model.delete_profile(cfg, "travel")
        self.assertEqual(model.profile_names(cfg), ["default"])

    def test_profile_errors(self):
        cfg = model.normalize({})
        for call in (
            lambda: model.use_profile(cfg, "nope"),
            lambda: model.create_profile(cfg, "default"),
            lambda: model.create_profile(cfg, "x", "nope"),
            lambda: model.delete_profile(cfg, "default"),
            lambda: model.delete_profile(cfg, "nope"),
        ):
            with self.assertRaises(UsageError):
                call()


class StoreTests(unittest.TestCase):
    def setUp(self):
        isolate_paths(self)

    def test_round_trip_splits_secrets(self):
        cfg = model.normalize({})
        model.set_field(cfg, "password", "hunter2hunter2")
        store.save(cfg)
        self.assertNotIn("hunter2hunter2", paths.CONFIG_PATH.read_text())
        self.assertEqual(stat.S_IMODE(paths.SECRETS_PATH.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(paths.CONFIG_PATH.stat().st_mode), 0o644)
        self.assertEqual(store.load()["password"], "hunter2hunter2")

    def test_missing_files_give_defaults(self):
        self.assertEqual(store.load()["ssid"], "apsta-hotspot")

    def test_legacy_config_migrated_as_root(self):
        as_root(self)
        paths.CONFIG_DIR.mkdir(parents=True)
        paths.CONFIG_PATH.write_text(json.dumps({"ssid": "Old", "password": "legacy-pass", "start_method": "hostapd"}))
        cfg = store.load()
        self.assertEqual(cfg["password"], "legacy-pass")
        on_disk = json.loads(paths.CONFIG_PATH.read_text())
        self.assertNotIn("password", json.dumps(on_disk))
        self.assertNotIn("start_method", on_disk)
        self.assertEqual(json.loads(paths.SECRETS_PATH.read_text())["default"], "legacy-pass")

    def test_corrupt_config_is_backed_up_not_lost(self):
        as_root(self)
        paths.CONFIG_DIR.mkdir(parents=True)
        paths.CONFIG_PATH.write_text("{not json")
        with mock.patch("sys.stderr"):
            cfg = store.load()
        self.assertEqual(cfg["ssid"], "apsta-hotspot")
        backups = list(paths.CONFIG_DIR.glob("config.json.corrupt-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "{not json")

    def test_corrupt_secrets(self):
        paths.CONFIG_DIR.mkdir(parents=True)
        paths.SECRETS_PATH.write_text("[[[")
        with mock.patch("sys.stderr"):
            self.assertIsNone(store.load()["password"])

    @unittest.skipIf(os.geteuid() == 0, "root can read any file")
    def test_unreadable_secrets_hide_password(self):
        cfg = model.normalize({})
        model.set_field(cfg, "password", "abcdefgh1")
        store.save(cfg)
        paths.SECRETS_PATH.chmod(0)
        self.assertIsNone(store.load()["password"])

    def test_generate_password(self):
        pw = store.generate_password()
        self.assertEqual(len(pw), 16)
        validate.password(pw)
        self.assertFalse(set(pw) & set("Il1O0o"))
        self.assertNotEqual(pw, store.generate_password())


if __name__ == "__main__":
    unittest.main()
