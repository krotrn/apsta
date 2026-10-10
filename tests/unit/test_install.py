import hashlib
import io
import tarfile
import tempfile
import unittest
import unittest.mock as mock
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from apsta_cli import __version__, cli
from apsta_cli.core import paths, shell
from apsta_cli.core.errors import SetupError
from apsta_cli.services import installation
from apsta_cli.services.installation import Installation, Release
from tests.support import FakeShell, as_root, isolate_paths

NEWER = "99.0.0"
SUMS = f"""\
{"a" * 64}  apsta-{NEWER}-1-any.pkg.tar.zst
{"b" * 64}  apsta-{NEWER}.tar.gz
{"c" * 64}  apsta_{NEWER}-1_all.deb
"""


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class DetectTests(unittest.TestCase):
    def setUp(self):
        self.sh = FakeShell(tools=["dpkg", "pacman"]).install(self)
        self.sh.on("dpkg", rc=1).on("pacman", rc=1)

    def test_install_sh_lives_in_prefix_lib_apsta(self):
        inst = installation.detect(Path("/opt/x/lib/apsta"))
        self.assertEqual((inst.method, inst.prefix), ("source", Path("/opt/x")))

    def test_package_managers_are_asked_who_owns_the_module(self):
        self.sh.on("dpkg", "-S", rc=0)
        self.assertEqual(installation.detect(Path("/usr/lib/python3/dist-packages")).method, "deb")
        self.assertTrue(self.sh.called("dpkg", "-S", "/usr/lib/python3/dist-packages/apsta_cli/__init__.py"))
        self.sh.on("dpkg", "-S", rc=1).on("pacman", "-Qo", rc=0)
        self.assertEqual(installation.detect(Path("/usr/lib/python3.14/site-packages")).method, "pacman")

    def test_git_checkout_and_pip(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".git").mkdir()
            self.assertEqual(installation.detect(Path(tmp)).method, "checkout")
        self.assertEqual(installation.detect(Path("/home/u/.local/lib/python3.12/site-packages")).method, "pip")


class ReleaseTests(unittest.TestCase):
    def test_latest_release_reads_sha256sums(self):
        with mock.patch.object(installation, "_download", return_value=SUMS.encode()):
            release = installation.latest_release()
        self.assertEqual(release.version, NEWER)
        self.assertEqual(release.asset("_all.deb"), f"apsta_{NEWER}-1_all.deb")

    def test_versions_compare_numerically(self):
        self.assertTrue(installation.is_newer("0.12.0", "0.11.10"))
        self.assertTrue(installation.is_newer("0.11.10", "0.11.9"))
        self.assertFalse(installation.is_newer("0.11.1", "0.11.1"))

    def test_download_asset_checks_the_sum(self):
        data = b"package"
        release = Release("1.0", {"a.deb": hashlib.sha256(data).hexdigest(), "b.deb": "0" * 64})
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(installation, "_download", return_value=data):
            self.assertEqual(installation.download_asset(release, "a.deb", Path(tmp)).read_bytes(), data)
            with self.assertRaises(SetupError):
                installation.download_asset(release, "b.deb", Path(tmp))

    def test_fetch_source_extracts_the_tag(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo("apsta-1.0/install.sh")
            info.size = 3
            tar.addfile(info, io.BytesIO(b"#!\n"))
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(installation, "_download", return_value=buf.getvalue()),
        ):
            tree = installation.fetch_source("1.0", Path(tmp))
            self.assertTrue((tree / "install.sh").is_file())

    def test_download_failure_is_a_setup_error(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("no network")), self.assertRaises(SetupError):
            installation.latest_release()


class PacmanConfTests(unittest.TestCase):
    CONF = "[options]\nHoldPkg = pacman\n\n[core]\nInclude = /etc/pacman.d/mirrorlist\n"
    REPO = "\n[apsta]\nSigLevel = Optional TrustAll\nServer = https://github.com/krotrn/apsta/releases/download/arch-repo\n"

    def test_removes_the_appended_section(self):
        self.assertEqual(installation.without_pacman_repo(self.CONF + self.REPO), self.CONF)

    def test_keeps_what_follows_it(self):
        text = self.CONF + self.REPO + "\n#[custom]\n#Server = file:///home/custompkgs\n\n[extra]\nInclude = x\n"
        result = installation.without_pacman_repo(text)
        self.assertNotIn("apsta", result)
        self.assertIn("#[custom]\n#Server = file:///home/custompkgs\n", result)
        self.assertIn("[extra]\nInclude = x\n", result)

    def test_unchanged_without_the_section(self):
        self.assertEqual(installation.without_pacman_repo(self.CONF), self.CONF)


class UpdateTests(unittest.TestCase):
    def setUp(self):
        isolate_paths(self)
        as_root(self)
        self.sh = FakeShell().install(self)
        self.sh.on("/usr/bin/apsta", "--version", stdout=f"apsta {NEWER}\n")
        self.visible = mock.patch.object(shell, "run_visible", return_value=shell.Result([], 0, "", "")).start()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(installation, "_download", return_value=SUMS.encode()).start()
        mock.patch("apsta_cli.cmd.install.apsta_binary", return_value="/usr/bin/apsta").start()

    def installed(self, method, root=Path("/usr/lib/python3/dist-packages")):
        mock.patch.object(installation, "detect", return_value=Installation(method, root)).start()

    def commands(self):
        return [[str(a) for a in c.args[0]] for c in self.visible.call_args_list]

    def test_pip_and_checkout_point_elsewhere(self):
        self.installed("pip")
        code, _, err = run_cli("update")
        self.assertEqual(code, 1)
        self.assertIn("pipx upgrade", err)
        self.visible.assert_not_called()

    def test_up_to_date(self):
        self.installed("pacman")
        with mock.patch.object(
            installation, "_download", return_value=f"{'b' * 64}  apsta-{__version__}.tar.gz".encode()
        ):
            code, out, _ = run_cli("update")
        self.assertEqual(code, 0)
        self.assertIn("latest version", out)
        self.visible.assert_not_called()

    def test_pacman(self):
        self.installed("pacman")
        code, out, _ = run_cli("upgrade")
        self.assertEqual(code, 0)
        self.assertEqual(self.commands(), [["pacman", "-Sy", "--needed", "--noconfirm", "apsta"]])
        self.assertIn(f"apsta {NEWER} installed", out)

    def test_deb_from_the_ppa(self):
        self.installed("deb")
        self.sh.on("apt-cache", "policy", stdout="500 https://ppa.launchpadcontent.net/krotrn/apsta/ubuntu noble/main")
        run_cli("update")
        self.assertEqual(self.commands()[-1], ["apt-get", "install", "-y", "--only-upgrade", "apsta"])

    def test_deb_from_the_release_is_verified(self):
        self.installed("deb")
        with mock.patch.object(installation, "download_asset", return_value=Path("/tmp/x/apsta.deb")) as download:
            run_cli("update")
        self.assertEqual(download.call_args.args[1], f"apsta_{NEWER}-1_all.deb")
        self.assertEqual(self.commands(), [["apt-get", "install", "-y", "/tmp/x/apsta.deb"]])

    def test_package_source_behind_the_release(self):
        self.installed("pacman")
        self.sh.on("/usr/bin/apsta", "--version", stdout="apsta 0.0.1\n")
        _, out, err = run_cli("update")
        self.assertIn("still has apsta 0.0.1", out + err)

    def test_source_reruns_install_sh_with_the_same_prefix(self):
        self.installed("source", Path("/opt/x/lib/apsta"))
        with mock.patch.object(installation, "fetch_source", return_value=Path("/tmp/s/apsta")):
            run_cli("update")
        self.assertEqual(self.commands(), [["bash", "/tmp/s/apsta/install.sh"]])
        self.assertEqual(self.visible.call_args.kwargs["env"], {"PREFIX": "/opt/x"})

    def test_needs_root(self):
        self.installed("pacman")
        with mock.patch("os.geteuid", return_value=1000):
            self.assertEqual(run_cli("update")[0], 4)


class UninstallTests(unittest.TestCase):
    def setUp(self):
        root = isolate_paths(self)
        as_root(self)
        self.sh = FakeShell().install(self)
        self.visible = mock.patch.object(shell, "run_visible", return_value=shell.Result([], 0, "", "")).start()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(installation, "EXTRA_FILES", (root / "bash_completion.d" / "apsta",)).start()
        self.disable = mock.patch("apsta_cli.cmd.install.disable_boot_service").start()
        self.present = mock.patch("apsta_cli.cmd.install.boot_service_present", return_value=False).start()
        self.conf = root / "pacman.conf"
        mock.patch.object(installation, "PACMAN_CONF", self.conf).start()
        paths.CONFIG_DIR.mkdir(parents=True)
        paths.CONFIG_PATH.write_text("{}")
        paths.LOG_PATH.write_text("log\n")
        mock.patch("apsta_cli.core.log._stopped", False).start()  # uninstall stops logging for the process

    def installed(self, method, root=Path("/usr/lib/python3/dist-packages")):
        mock.patch.object(installation, "detect", return_value=Installation(method, root)).start()

    def test_asks_first(self):
        self.installed("pacman")
        with mock.patch("sys.stdin.isatty", return_value=True), mock.patch("builtins.input", return_value="n"):
            self.assertEqual(run_cli("uninstall")[0], 1)
        self.visible.assert_not_called()
        self.assertTrue(paths.CONFIG_PATH.exists())

    def test_needs_yes_without_a_terminal(self):
        self.installed("pacman")
        with mock.patch("sys.stdin.isatty", return_value=False):
            self.assertEqual(run_cli("uninstall")[0], 2)
        self.visible.assert_not_called()

    def test_pacman_removes_package_repository_and_settings(self):
        self.installed("pacman")
        self.conf.write_text(PacmanConfTests.CONF + PacmanConfTests.REPO)
        code, out, _ = run_cli("uninstall", "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(self.visible.call_args.args[0], ["pacman", "-Rns", "--noconfirm", "apsta"])
        self.assertEqual(self.conf.read_text(), PacmanConfTests.CONF)
        self.assertFalse(paths.CONFIG_DIR.exists())
        self.assertFalse(paths.LOG_PATH.exists())
        self.assertIn("removed completely", out)

    def test_keep_config(self):
        self.installed("deb")
        code, out, _ = run_cli("uninstall", "-y", "--keep-config")
        self.assertEqual(code, 0)
        self.assertEqual(self.visible.call_args.args[0], ["apt-get", "purge", "-y", "apsta"])
        self.assertTrue(paths.CONFIG_PATH.exists())
        self.assertFalse(paths.LOG_PATH.exists())

    def test_ppa_is_removed_too(self):
        self.installed("deb")
        self.sh.tools.add("add-apt-repository")
        self.sh.on("apt-cache", "policy", stdout="500 https://ppa.launchpadcontent.net/krotrn/apsta/ubuntu noble/main")
        run_cli("uninstall", "-y")
        self.assertEqual(self.visible.call_args.args[0], ["add-apt-repository", "--remove", "-y", "ppa:krotrn/apsta"])

    def test_source_runs_a_copy_of_install_sh(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = Path(tmp) / "lib" / "apsta"
            lib.mkdir(parents=True)
            (lib / "install.sh").write_text("#!/bin/bash\n")
            self.installed("source", lib)
            run_cli("uninstall", "-y")
            argv = [str(a) for a in self.visible.call_args.args[0]]
        self.assertEqual(argv[0], "bash")
        self.assertNotEqual(argv[1], str(lib / "install.sh"))  # it deletes the installed one
        self.assertEqual(argv[2:], ["--uninstall"])
        self.assertEqual(self.visible.call_args.kwargs["env"], {"PREFIX": tmp})

    def test_pip_cleans_up_and_says_how_to_remove_the_package(self):
        self.installed("pip")
        code, _, err = run_cli("uninstall", "-y")
        self.assertEqual(code, 0)
        self.visible.assert_not_called()
        self.assertIn("pipx uninstall", err)
        self.assertFalse(paths.CONFIG_DIR.exists())

    def test_package_manager_failure_stops_before_deleting_settings(self):
        self.installed("pacman")
        self.visible.return_value = shell.Result(["pacman"], 1, "", "")
        code, _, _ = run_cli("uninstall", "-y")
        self.assertEqual(code, 1)
        self.assertTrue(paths.CONFIG_PATH.exists())

    def test_boot_service_is_turned_off_only_when_there_is_one(self):
        self.installed("pip")
        run_cli("uninstall", "-y")
        self.disable.assert_not_called()
        self.present.return_value = True
        run_cli("uninstall", "-y")
        self.disable.assert_called_once()
