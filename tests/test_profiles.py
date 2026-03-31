import unittest

from apsta_cli.common import (
    create_profile,
    delete_profile,
    get_active_profile_name,
    normalize_config,
    set_active_profile,
    set_profile_field,
)


class ProfileConfigTests(unittest.TestCase):
    def test_legacy_config_is_migrated_to_default_profile(self):
        cfg = normalize_config({
            "ssid": "LegacySSID",
            "password": "legacy-pass",
            "band": "a",
            "channel": "44",
            "interface": "wlan1",
        })

        self.assertEqual(cfg["active_profile"], "default")
        self.assertIn("default", cfg["profiles"])
        self.assertEqual(cfg["profiles"]["default"]["ssid"], "LegacySSID")
        self.assertEqual(cfg["ssid"], "LegacySSID")
        self.assertEqual(cfg["interface"], "wlan1")

    def test_switching_active_profile_syncs_top_level_fields(self):
        cfg = normalize_config({
            "profiles": {
                "default": {
                    "ssid": "Home",
                    "password": "home-pass",
                    "band": "bg",
                    "channel": "1",
                    "interface": None,
                },
                "travel": {
                    "ssid": "Travel",
                    "password": "travel-pass",
                    "band": "a",
                    "channel": "36",
                    "interface": "wlan2",
                },
            },
            "active_profile": "default",
        })

        self.assertTrue(set_active_profile(cfg, "travel"))
        self.assertEqual(get_active_profile_name(cfg), "travel")
        self.assertEqual(cfg["ssid"], "Travel")
        self.assertEqual(cfg["interface"], "wlan2")

    def test_set_profile_field_updates_active_profile_and_root_fields(self):
        cfg = normalize_config({})

        set_profile_field(cfg, "ssid", "CafeHotspot")
        self.assertEqual(cfg["profiles"]["default"]["ssid"], "CafeHotspot")
        self.assertEqual(cfg["ssid"], "CafeHotspot")

        set_profile_field(cfg, "interface", "none")
        self.assertIsNone(cfg["profiles"]["default"]["interface"])
        self.assertIsNone(cfg["interface"])

    def test_create_and_delete_profile_constraints(self):
        cfg = normalize_config({})

        self.assertTrue(create_profile(cfg, "work"))
        self.assertIn("work", cfg["profiles"])

        self.assertTrue(set_active_profile(cfg, "work"))
        self.assertFalse(delete_profile(cfg, "work"))

        self.assertTrue(set_active_profile(cfg, "default"))
        self.assertTrue(delete_profile(cfg, "work"))
        self.assertNotIn("work", cfg["profiles"])


if __name__ == "__main__":
    unittest.main()
