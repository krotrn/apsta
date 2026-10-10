"""get.sh, the one-command installer: which route it takes on each distribution."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

GET = Path(__file__).resolve().parents[2] / "get.sh"
if not GET.exists():
    # Distribution package builds (e.g. Debian's pybuild) copy only the package and tests.
    raise unittest.SkipTest("get.sh is not part of this tree")


@unittest.skipUnless(shutil.which("sh"), "needs a POSIX shell")
class GetScriptTests(unittest.TestCase):
    def dry_run(self, os_release, *args, display=False):
        with tempfile.NamedTemporaryFile("w", suffix="os-release") as f:
            f.write(os_release)
            f.flush()
            env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
            env["APSTA_OS_RELEASE"] = f.name
            if display:
                env["WAYLAND_DISPLAY"] = "wayland-0"
            result = subprocess.run(
                ["sh", str(GET), "--dry-run", *args], env=env, capture_output=True, text=True, check=False
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        return [line[2:] for line in result.stdout.splitlines() if line.startswith("+ ")]

    def test_arch_uses_the_pacman_repository(self):
        commands = self.dry_run("ID=endeavouros\nID_LIKE=arch\n")
        self.assertEqual(commands[-1], "sudo pacman -Sy --needed --noconfirm apsta hostapd dnsmasq")

    def test_ubuntu_derivatives_use_the_ppa(self):
        commands = self.dry_run('ID=linuxmint\nID_LIKE="ubuntu debian"\n')
        self.assertIn("sudo add-apt-repository -y ppa:krotrn/apsta", commands)
        self.assertEqual(commands[-1], "sudo apt-get install -y apsta")

    def test_debian_uses_the_release_deb(self):
        commands = self.dry_run("ID=debian\n")
        self.assertIn("download and verify apsta_VERSION-1_all.deb", commands)

    def test_other_distributions_install_from_source_with_their_dependencies(self):
        commands = self.dry_run('ID="opensuse-tumbleweed"\nID_LIKE="opensuse suse"\n')
        self.assertEqual(commands[0], "sudo zypper --non-interactive install python3 NetworkManager iw iproute2")
        self.assertEqual(commands[-1], "sudo bash apsta-VERSION/install.sh")

    def test_source_flag_skips_packages(self):
        commands = self.dry_run("ID=arch\n", "--source")
        self.assertEqual(commands[0], "sudo pacman -Sy --needed --noconfirm python networkmanager iw iproute2")
        self.assertNotIn("apsta", commands[0].split())
        self.assertEqual(commands[-1], "sudo bash apsta-VERSION/install.sh")

    def test_desktop_libraries_follow_the_session(self):
        self.assertNotIn("gtk4", " ".join(self.dry_run("ID=arch\n")))
        self.assertIn("gtk4", " ".join(self.dry_run("ID=arch\n", display=True)))
        self.assertNotIn("gtk4", " ".join(self.dry_run("ID=arch\n", "--cli-only", display=True)))
        self.assertIn("gtk4", " ".join(self.dry_run("ID=arch\n", "--gui")))

    def test_unknown_option_fails(self):
        result = subprocess.run(["sh", str(GET), "--bogus"], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Unknown option", result.stderr)
