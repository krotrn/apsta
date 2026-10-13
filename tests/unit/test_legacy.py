import io
import json
import unittest
import unittest.mock as mock
from contextlib import redirect_stderr, redirect_stdout

from tests.support import FakeShell, isolate_paths
from twinfi_cli import cli, legacy
from twinfi_cli.core import paths
from twinfi_cli.services import installation


def migrate():
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        legacy.migrate()
    return out.getvalue() + err.getvalue()


class MigrateTests(unittest.TestCase):
    def setUp(self):
        self.root = isolate_paths(self)
        self.sh = FakeShell(tools=["systemctl"]).install(self)
        self.sh.on("systemctl", "is-enabled", rc=1)
        self.enable = mock.patch("twinfi_cli.cmd.service._enable_systemd").start()
        mock.patch("twinfi_cli.cmd.service.twinfi_binary", return_value="/usr/bin/twinfi").start()
        self.addCleanup(mock.patch.stopall)

    def test_nothing_to_move(self):
        self.assertEqual(migrate(), "")
        self.assertEqual(self.sh.calls, [])

    def test_settings_move_to_the_new_directory(self):
        paths.LEGACY_CONFIG_DIR.mkdir()
        (paths.LEGACY_CONFIG_DIR / "config.json").write_text(json.dumps({"active_profile": "home"}))
        self.assertIn("Moved the apsta settings", migrate())
        self.assertFalse(paths.LEGACY_CONFIG_DIR.exists())
        self.assertEqual(json.loads(paths.CONFIG_PATH.read_text()), {"active_profile": "home"})

    def test_settings_move_into_the_empty_directory_systemd_made(self):
        paths.LEGACY_CONFIG_DIR.mkdir()
        (paths.LEGACY_CONFIG_DIR / "secrets.json").write_text("{}")
        paths.CONFIG_DIR.mkdir()
        migrate()
        self.assertTrue(paths.SECRETS_PATH.exists())
        self.assertFalse(paths.LEGACY_CONFIG_DIR.exists())

    def test_read_only_etc_postpones_the_move(self):
        paths.LEGACY_CONFIG_DIR.mkdir()
        with mock.patch("shutil.move", side_effect=OSError(30, "Read-only file system")):
            self.assertEqual(migrate(), "")
        self.assertTrue(paths.LEGACY_CONFIG_DIR.exists())

    def test_existing_twinfi_settings_win(self):
        paths.LEGACY_CONFIG_DIR.mkdir()
        paths.CONFIG_DIR.mkdir()
        paths.CONFIG_PATH.write_text("{}")
        migrate()
        self.assertTrue(paths.LEGACY_CONFIG_DIR.exists())
        self.assertEqual(paths.CONFIG_PATH.read_text(), "{}")

    def test_enabled_systemd_unit_is_enabled_under_the_new_name_without_starting(self):
        legacy.SYSTEMD_WANTS_LINK.parent.mkdir(parents=True)
        legacy.SYSTEMD_LOCAL_UNIT.write_text("[Unit]\n")
        legacy.SYSTEMD_WANTS_LINK.symlink_to(legacy.SYSTEMD_LOCAL_UNIT)
        migrate()
        self.assertTrue(self.sh.called("systemctl", "disable", "apsta.service"))
        self.assertFalse(legacy.SYSTEMD_LOCAL_UNIT.exists())
        self.assertFalse(legacy.SYSTEMD_WANTS_LINK.is_symlink())
        self.enable.assert_called_once_with("/usr/bin/twinfi", now=False)

    def test_dangling_link_left_by_the_removed_package(self):
        legacy.SYSTEMD_WANTS_LINK.parent.mkdir(parents=True)
        legacy.SYSTEMD_WANTS_LINK.symlink_to(self.root / "gone" / "apsta.service")
        migrate()
        self.assertFalse(self.sh.called("systemctl", "disable"))  # nothing left to disable
        self.assertFalse(legacy.SYSTEMD_WANTS_LINK.is_symlink())
        self.enable.assert_called_once()

    def test_already_enabled_twinfi_is_left_alone(self):
        self.sh.on("systemctl", "is-enabled", rc=0)
        legacy.SYSTEMD_WANTS_LINK.parent.mkdir(parents=True)
        legacy.SYSTEMD_WANTS_LINK.symlink_to(self.root / "gone")
        migrate()
        self.enable.assert_not_called()

    def test_disabled_local_unit_is_only_removed(self):
        legacy.SYSTEMD_LOCAL_UNIT.parent.mkdir(parents=True)
        legacy.SYSTEMD_LOCAL_UNIT.write_text("[Unit]\n")
        migrate()
        self.assertFalse(legacy.SYSTEMD_LOCAL_UNIT.exists())
        self.enable.assert_not_called()

    def test_openrc_service_is_removed_with_a_hint(self):
        legacy.OPENRC_SCRIPT.parent.mkdir(parents=True)
        legacy.OPENRC_SCRIPT.write_text("#!/sbin/openrc-run\n")
        text = migrate()
        self.assertTrue(self.sh.called("rc-update", "del", "apsta", "default"))
        self.assertFalse(legacy.OPENRC_SCRIPT.exists())
        self.assertIn("sudo twinfi enable", text)

    def test_runit_service_is_removed_with_a_hint(self):
        service_dir = self.root / "runsvdir"
        service_dir.mkdir()
        legacy.RUNIT_DIR.mkdir(parents=True)
        (legacy.RUNIT_DIR / "run").write_text("#!/bin/sh\n")
        (service_dir / "apsta").symlink_to(legacy.RUNIT_DIR)
        with mock.patch("twinfi_cli.cmd.service.RUNIT_SERVICE_DIRS", (service_dir,)):
            text = migrate()
        self.assertFalse((service_dir / "apsta").is_symlink())
        self.assertFalse(legacy.RUNIT_DIR.exists())
        self.assertIn("sudo twinfi enable", text)

    def test_stray_files_are_removed_unless_a_package_owns_them(self):
        stray = legacy.STRAY_FILES[0]
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_text("#!/bin/sh\n")
        self.sh.tools.add("dpkg")
        self.sh.on("dpkg", "-S", rc=0)
        migrate()
        self.assertTrue(stray.exists())
        self.sh.on("dpkg", "-S", rc=1)
        migrate()
        self.assertFalse(stray.exists())

    def test_pacman_section_is_renamed(self):
        conf = installation.PACMAN_CONF
        conf.parent.mkdir(parents=True, exist_ok=True)
        conf.write_text("[core]\nInclude = x\n\n[apsta]\nServer = https://example/arch-repo\n")
        self.assertIn("[twinfi]", migrate())
        self.assertEqual(conf.read_text(), "[core]\nInclude = x\n\n[twinfi]\nServer = https://example/arch-repo\n")

    def test_pacman_section_left_when_twinfi_is_there_too(self):
        conf = installation.PACMAN_CONF
        conf.parent.mkdir(parents=True, exist_ok=True)
        text = "[apsta]\nServer = a\n\n[twinfi]\nServer = a\n"
        conf.write_text(text)
        migrate()
        self.assertEqual(conf.read_text(), text)

    def test_hotspot_started_by_apsta_is_reported(self):
        paths.LEGACY_RUN_DIR.mkdir()
        (paths.LEGACY_RUN_DIR / "state.json").write_text("{}")
        self.assertIn("Restart the computer", migrate())


class EntryPointTests(unittest.TestCase):
    def setUp(self):
        isolate_paths(self)
        FakeShell().install(self)

    def test_runs_before_root_commands_only(self):
        with mock.patch.object(legacy, "migrate") as migrate_, redirect_stdout(io.StringIO()):
            with mock.patch("os.geteuid", return_value=1000):  # package builds run the tests as root
                cli.main(["config"])
            migrate_.assert_not_called()
            with mock.patch("os.geteuid", return_value=0):
                cli.main(["config"])
            migrate_.assert_called_once()

    def test_apsta_command_reminds_of_the_new_name(self):
        err = io.StringIO()
        with (
            mock.patch("sys.argv", ["apsta", "--version"]),
            redirect_stdout(io.StringIO()),
            redirect_stderr(err),
            self.assertRaises(SystemExit),
        ):
            cli.run_legacy()
        self.assertIn("now called twinfi", err.getvalue())


if __name__ == "__main__":
    unittest.main()
