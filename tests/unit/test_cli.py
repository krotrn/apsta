import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from apsta_cli import cli
from apsta_cli.cmd import completion, detect, service, usb
from apsta_cli.core import paths
from apsta_cli.hw import capability
from apsta_cli.hw.capability import HardwareCapability
from apsta_cli.hw.interfaces import WifiInterface
from apsta_cli.hw.usb import USB_CHIPSET_DB, UsbWifiDevice
from tests.support import FakeShell, as_root, isolate_paths


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class CliTests(unittest.TestCase):
    def test_no_command_prints_help(self):
        code, out, _ = run_cli()
        self.assertEqual(code, 0)
        self.assertIn("usage: apsta", out)

    def test_missing_tools(self):
        FakeShell([]).install(self)
        code, _, err = run_cli("detect")
        self.assertEqual(code, 1)
        self.assertIn("Missing required tools: iw", err)

    def test_keyboard_interrupt(self):
        with mock.patch.object(cli, "_handler", return_value=mock.Mock(side_effect=KeyboardInterrupt)):
            self.assertEqual(run_cli("stop")[0], 130)

    def test_run_entry_exits(self):
        with mock.patch.object(cli, "main", return_value=3), self.assertRaises(SystemExit) as ctx:
            cli.run()
        self.assertEqual(ctx.exception.code, 3)

    def test_force_is_alias_for_allow_disconnect(self):
        args = cli.build_parser().parse_args(["start", "--force"])
        self.assertTrue(args.allow_disconnect)


class CompletionTests(unittest.TestCase):
    def test_spec_covers_every_command_and_nested_action(self):
        spec = completion.spec(cli.build_parser())
        self.assertIn("clients", spec)
        self.assertEqual(spec["profile"][2], ["list", "show", "use", "create", "delete"])
        self.assertEqual(spec["completion"][2], ["bash", "zsh", "fish"])
        self.assertIn("--allow-disconnect", spec["start"][1])
        self.assertNotIn("--disconnect", spec["status"][1])  # hidden deprecated flag

    def test_generators(self):
        for shell_name in ("bash", "zsh", "fish"):
            code, out, _ = run_cli("completion", shell_name)
            self.assertEqual(code, 0)
            self.assertIn("recommend", out)
        self.assertIn("complete -F _apsta apsta", run_cli("completion", "bash")[1])
        self.assertIn("#compdef apsta", run_cli("completion", "zsh")[1])


class DetectVerdictTests(unittest.TestCase):
    def cap(self, ap, ap_sta):
        return HardwareCapability(
            "wlo1", "phy0", ap, True, ap_sta, True, 1, ["managed"], ["#{ managed } <= 1"], "iwlwifi", "Intel"
        )

    def test_verdicts(self):
        self.assertEqual(detect.verdict(self.cap(True, True))["mode"], "ap+sta")
        self.assertEqual(detect.verdict(self.cap(True, False))["mode"], "single")
        self.assertEqual(detect.verdict(self.cap(False, False))["mode"], "unsupported")

    def test_text_output_for_single_radio_with_usb_adapter(self):
        iface = WifiInterface("wlo1", "aa", "phy0", "managed", "UP", None)
        dongle = UsbWifiDevice("0e8d", "7961", "MediaTek", "wlx1", "mt7921u", USB_CHIPSET_DB[0])
        FakeShell([]).install(self)
        with (
            mock.patch.object(detect.interfaces, "client_interfaces", return_value=[iface]),
            mock.patch.object(detect.capability, "probe", return_value=self.cap(True, False)),
            mock.patch.object(detect.usb, "scan_usb_wifi", return_value=[dongle]),
            mock.patch.object(detect.shell, "have", return_value=False),
        ):
            with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
                code = detect.cmd_detect(SimpleNamespace(json=False))
        self.assertIn("compatible USB adapter", out.getvalue())
        self.assertEqual(code, 0)

    def test_text_output_variants(self):
        iface = WifiInterface("wlo1", "aa", "phy0", "managed", "UP", "Home")
        for cap, scan in ((self.cap(True, False), []), (self.cap(False, False), [])):
            with (
                mock.patch.object(detect.interfaces, "client_interfaces", return_value=[iface]),
                mock.patch.object(detect.capability, "probe", return_value=cap),
                mock.patch.object(detect.usb, "scan_usb_wifi", return_value=scan),
                redirect_stdout(io.StringIO()) as out,
                redirect_stderr(io.StringIO()),
            ):
                detect.cmd_detect(SimpleNamespace(json=False))
            self.assertIn("Verdict", out.getvalue())

    def test_no_interfaces(self):
        with mock.patch.object(detect.interfaces, "client_interfaces", return_value=[]):
            from apsta_cli.core.errors import HardwareError

            with self.assertRaises(HardwareError):
                detect.cmd_detect(SimpleNamespace(json=False))


class CapabilitySysfsTests(unittest.TestCase):
    def test_driver_and_pci_chipset(self):
        root = isolate_paths(self)
        pci = root / "devices" / "0000:02:00.0"
        (pci / "driver_target" / "iwlwifi").mkdir(parents=True)
        (pci / "vendor").write_text("0x8086")
        (pci / "class").write_text("0x028000")
        net = paths.SYSFS_NET / "wlo1"
        net.mkdir(parents=True)
        (net / "device").symlink_to(pci)
        (pci / "driver").symlink_to(pci / "driver_target" / "iwlwifi")
        FakeShell().on("lspci", "-s", "0000:02:00.0", stdout="02:00.0 Network controller: Intel Wi-Fi 6 AX200").install(
            self
        )
        self.assertEqual(capability._driver("wlo1"), "iwlwifi")
        self.assertEqual(capability._chipset("wlo1"), "Intel Wi-Fi 6 AX200")

    def test_usb_chipset(self):
        root = isolate_paths(self)
        dev = root / "devices" / "1-2"
        (dev / "1-2:1.0").mkdir(parents=True)
        (dev / "idVendor").write_text("0e8d\n")
        (dev / "idProduct").write_text("7961\n")
        net = paths.SYSFS_NET / "wlx1"
        net.mkdir(parents=True)
        (net / "device").symlink_to(dev / "1-2:1.0")
        FakeShell().on(
            "lsusb", "-d", "0e8d:7961", stdout="Bus 001 Device 004: ID 0e8d:7961 MediaTek Wireless_Device"
        ).install(self)
        self.assertEqual(capability._chipset("wlx1"), "MediaTek Wireless_Device")


class UsbCommandTests(unittest.TestCase):
    def setUp(self):
        self.out = io.StringIO()
        for p in (redirect_stdout(self.out), redirect_stderr(io.StringIO())):
            p.__enter__()
            self.addCleanup(p.__exit__, None, None, None)

    def test_scan_variants(self):
        known = UsbWifiDevice("0e8d", "7961", "MediaTek", "wlx1", "mt7921u", USB_CHIPSET_DB[0])
        no_iface = UsbWifiDevice("0e8d", "7961", "MediaTek", None, None, USB_CHIPSET_DB[0])
        unknown = UsbWifiDevice("abcd", "0001", "Some WLAN", None, None, None)
        with (
            mock.patch.object(usb, "scan_usb_wifi", return_value=[known, no_iface, unknown]),
            mock.patch.object(usb, "_kernel_version", return_value="5.4"),
        ):
            usb.cmd_scan_usb(SimpleNamespace())
        text = self.out.getvalue()
        self.assertIn("mt7921au", text)
        self.assertIn("Unknown chipset", text)
        self.assertIn("upgrade your kernel", text)

    def test_scan_none(self):
        with mock.patch.object(usb, "scan_usb_wifi", return_value=[]):
            usb.cmd_scan_usb(SimpleNamespace())
        self.assertIn("No USB WiFi adapters", self.out.getvalue())

    def test_recommend_variants(self):
        iface = WifiInterface("wlo1", "aa", "phy0", "managed", "UP", None)
        good = mock.Mock(ap_sta=True)
        bad = mock.Mock(ap_sta=False)
        with mock.patch.object(usb.interfaces, "client_interfaces", return_value=[iface]):
            with mock.patch.object(usb.capability, "probe", return_value=good):
                usb.cmd_recommend(SimpleNamespace())
            self.assertIn("already supports", self.out.getvalue())
            known = UsbWifiDevice("0e8d", "7961", "MediaTek", None, None, USB_CHIPSET_DB[0])
            with (
                mock.patch.object(usb.capability, "probe", return_value=bad),
                mock.patch.object(usb, "scan_usb_wifi", return_value=[known]),
            ):
                usb.cmd_recommend(SimpleNamespace())
            self.assertIn("already have a compatible", self.out.getvalue())
            with (
                mock.patch.object(usb.capability, "probe", return_value=bad),
                mock.patch.object(usb, "scan_usb_wifi", return_value=[]),
            ):
                usb.cmd_recommend(SimpleNamespace())
            self.assertIn("Recommended USB Adapters", self.out.getvalue())

    def test_scan_sysfs(self):
        root = isolate_paths(self)
        from apsta_cli.hw import usb as hwusb

        dev = root / "usb" / "1-2"
        (dev / "1-2:1.0" / "net" / "wlx00").mkdir(parents=True)
        (dev / "1-2:1.0" / "driver_t").mkdir()
        (dev / "1-2:1.0" / "driver").symlink_to(dev / "1-2:1.0" / "driver_t")
        for name, value in (("idVendor", "0e8d"), ("idProduct", "7961"), ("busnum", "1"), ("devnum", "4")):
            (dev / name).write_text(value)
        other = root / "usb" / "1-3"
        other.mkdir()
        for name, value in (("idVendor", "046d"), ("idProduct", "c52b"), ("busnum", "1"), ("devnum", "5")):
            (other / name).write_text(value)
        FakeShell().on(
            "lsusb",
            stdout="Bus 001 Device 004: ID 0e8d:7961 MediaTek Wireless\nBus 001 Device 005: ID 046d:c52b Logitech Receiver",
        ).install(self)
        with mock.patch.object(
            hwusb, "Path", side_effect=lambda p: root / "usb" if p == "/sys/bus/usb/devices" else Path(p)
        ):
            devices = hwusb.scan_usb_wifi()
        self.assertEqual([(d.vid, d.interface, d.driver) for d in devices], [("0e8d", "wlx00", "driver_t")])


class ServiceCommandTests(unittest.TestCase):
    def setUp(self):
        root = isolate_paths(self)
        as_root(self)
        self.root = root
        for name in (
            "SYSTEMD_PACKAGED_UNIT",
            "SYSTEMD_LOCAL_UNIT",
            "SLEEP_HOOK",
            "PM_SLEEP_HOOK",
            "OPENRC_SCRIPT",
            "RUNIT_DIR",
        ):
            p = mock.patch.object(service, name, root / "svc" / name.lower())
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(service, "RUNIT_SERVICE_DIRS", (root / "runsvdir",))
        p.start()
        self.addCleanup(p.stop)
        for p in (redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            p.__enter__()
            self.addCleanup(p.__exit__, None, None, None)
        self.sh = FakeShell().install(self)

    def enable(self, init):
        with (
            mock.patch.object(service, "detect_init", return_value=init),
            mock.patch.object(service, "apsta_binary", return_value="/opt/apsta/bin/apsta"),
        ):
            return service.cmd_enable(SimpleNamespace())

    def disable(self, init):
        with mock.patch.object(service, "detect_init", return_value=init):
            return service.cmd_disable(SimpleNamespace())

    def test_systemd_local_install(self):
        self.assertEqual(self.enable("systemd"), 0)
        unit = service.SYSTEMD_LOCAL_UNIT.read_text()
        self.assertIn("ExecStart=/opt/apsta/bin/apsta run --wait-sta 30", unit)
        self.assertTrue(self.sh.called("systemctl", "enable", "--now", "apsta.service"))
        self.assertIn("/opt/apsta/bin/apsta", service.SLEEP_HOOK.read_text())
        self.disable("systemd")
        self.assertFalse(service.SYSTEMD_LOCAL_UNIT.exists())

    def test_systemd_packaged_unit_is_not_shadowed(self):
        service.SYSTEMD_PACKAGED_UNIT.parent.mkdir(parents=True, exist_ok=True)
        service.SYSTEMD_PACKAGED_UNIT.write_text("x")
        with (
            mock.patch.object(service, "detect_init", return_value="systemd"),
            mock.patch.object(service, "apsta_binary", return_value=service.PACKAGED_BINARY),
        ):
            service.cmd_enable(SimpleNamespace())
        self.assertFalse(service.SYSTEMD_LOCAL_UNIT.exists())

    def test_openrc(self):
        service.PM_SLEEP_HOOK.parent.mkdir(parents=True, exist_ok=True)
        self.enable("openrc")
        self.assertIn('command="/opt/apsta/bin/apsta"', service.OPENRC_SCRIPT.read_text())
        self.assertTrue(self.sh.called("rc-update", "add", "apsta", "default"))
        self.assertTrue(service.PM_SLEEP_HOOK.exists())
        self.disable("openrc")
        self.assertFalse(service.OPENRC_SCRIPT.exists())
        self.assertFalse(service.PM_SLEEP_HOOK.exists())

    def test_runit(self):
        (self.root / "runsvdir").mkdir()
        self.enable("runit")
        self.assertTrue((self.root / "runsvdir" / "apsta").is_symlink())
        self.disable("runit")
        self.assertFalse((self.root / "runsvdir" / "apsta").exists())

    def test_runit_without_service_dir(self):
        from apsta_cli.core.errors import ApstaError

        with self.assertRaises(ApstaError):
            self.enable("runit")

    def test_unknown_init(self):
        from apsta_cli.core.errors import ApstaError

        with self.assertRaises(ApstaError):
            self.enable("unknown")
        self.assertEqual(self.disable("unknown"), 0)

    def test_helpers(self):
        self.assertIn(service.detect_init(), ("systemd", "openrc", "runit", "unknown"))
        self.assertIn("[Service]", service.data_file("apsta.service"))
        with mock.patch("sys.argv", ["/x/y/apsta"]), mock.patch("os.access", return_value=True):
            self.assertEqual(service.apsta_binary(), "/x/y/apsta")
        with mock.patch("sys.argv", ["python -m"]):
            self.assertEqual(service.apsta_binary(), service.PACKAGED_BINARY)


if __name__ == "__main__":
    unittest.main()
